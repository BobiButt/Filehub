"""Auth API: join, approve/reject, kick, block, rotate code (§8 Auth)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api import ApiError, clean_name, current_device, require_host
from app.config import get_config
from app.core.broadcaster import broadcaster
from app.core.connection_manager import manager
from app.core.hub import hub
from app.core.security import SESSION_COOKIE, security
from app.models.schemas import ApproveRequest, DeviceRequest, DeviceState, JoinRequest

router = APIRouter()
config = get_config()


def _join_error(
    message: str, *, attempts: int | None = None, cooldown: int | None = None
) -> JSONResponse:
    payload: dict = {"error": message}
    if attempts is not None:
        payload["attempts_remaining"] = attempts
    if cooldown is not None:
        payload["cooldown_seconds"] = cooldown
    return JSONResponse(status_code=401, content=payload)


@router.post("/api/auth/join")
async def join(body: JoinRequest, request: Request, response: Response) -> dict:
    """Join with the 6-digit code; sets the HttpOnly session cookie."""
    name = clean_name(body.name)
    fingerprint = security.fingerprint(request, body.screen, body.tz)
    if security.is_blocked(fingerprint):
        raise ApiError(403, "this device is blocked")

    # Re-join with a live session is idempotent (page refresh within grace).
    existing = current_device(request)
    if existing is not None:
        response.set_cookie(SESSION_COOKIE, existing.id, httponly=True, samesite="lax")
        return {
            "device_id": existing.id,
            "role": existing.role,
            "state": existing.state.value,
        }

    is_host = security.is_host(request, hub.admin_token)
    if not is_host:
        ok, reason, cooldown = security.check_join_code(
            body.code, hub.join_code, fingerprint
        )
        if not ok:
            if reason == "cooldown":
                return _join_error("too many attempts", cooldown=cooldown)
            if reason == "blocked":
                raise ApiError(403, "this device is blocked")
            attempts, cooldown = security.track_wrong_attempt(fingerprint)
            if cooldown == 0 and security.attempts_left(fingerprint) <= 0:
                raise ApiError(403, "too many failed attempts; blocked until restart")
            base = config.wrong_code_attempts_before_cooldown
            left = 0 if attempts % base == 0 else base - (attempts % base)
            return _join_error("wrong join code", attempts=left, cooldown=cooldown or None)

    # Decide role + state server-side; never from client input (§12).
    if is_host:
        role, state = "host", DeviceState.APPROVED
    else:
        role = "guest"
        approved_guests = sum(
            1
            for d in hub.devices.values()
            if d.role == "guest" and d.state == DeviceState.APPROVED
        )
        if not config.require_host_approval:
            state = DeviceState.APPROVED
        elif config.persist_trusted_devices and security.is_trusted(fingerprint):
            state = DeviceState.APPROVED
        elif approved_guests < config.auto_approve_first_n:
            state = DeviceState.APPROVED
        else:
            state = DeviceState.PENDING

    device = hub.register_device(
        name=name,
        user_agent=request.headers.get("user-agent", ""),
        fingerprint=fingerprint,
        ip=request.client.host if request.client else "",
        role=role,
        state=state,
    )
    security.clear_attempts(fingerprint)
    response.set_cookie(SESSION_COOKIE, device.id, httponly=True, samesite="lax")
    await broadcaster.log(
        "auth", device.name, "SERVER", "approved" if state == DeviceState.APPROVED else "pending"
    )
    if state == DeviceState.PENDING:
        await broadcaster.device_pending(device)
    return {"device_id": device.id, "role": role, "state": state.value}


async def _remove_device(target_id: str, reason: str) -> None:
    """Targeted notice → unregister → broadcast (order matters, §12)."""
    ws = manager.sockets.get(target_id)
    if ws is not None:
        await manager.send_to(
            target_id,
            {"type": "device_kicked", "payload": {"device_id": target_id, "reason": reason}},
        )
    hub.unregister_device(target_id)
    if ws is not None:
        await manager.disconnect(target_id, ws)
    await broadcaster.device_kicked(target_id, reason)


@router.post("/api/auth/approve")
async def approve(body: ApproveRequest, request: Request) -> dict:
    """Host approves or rejects a pending device."""
    actor = require_host(request)
    target = hub.get_device(body.device_id)
    if target is None:
        raise ApiError(404, "unknown device")
    if body.action == "approve":
        if target.state != DeviceState.PENDING:
            raise ApiError(409, "device is not pending approval")
        hub.set_device_state(target.id, DeviceState.APPROVED)
        if config.persist_trusted_devices:
            security.add_trusted(target.fingerprint, target.name)
        await broadcaster.log("auth", actor, target.name, "approved")
        if manager.is_connected(target.id):
            await broadcaster.snapshot_to(target.id)  # guest sees the dashboard
        await broadcaster.device_joined(target, exclude=target.id)
        await broadcaster.scheduler_update()
        return {"ok": True, "state": DeviceState.APPROVED.value}

    if target.state != DeviceState.PENDING:
        raise ApiError(409, "device is not pending approval")
    await broadcaster.log("auth", actor, target.name, "rejected")
    await _remove_device(target.id, "rejected")
    return {"ok": True, "state": "rejected"}


@router.post("/api/auth/kick")
async def kick(body: DeviceRequest, request: Request) -> dict:
    """Host removes an approved device from the session."""
    actor = require_host(request)
    target = hub.get_device(body.device_id)
    if target is None:
        raise ApiError(404, "unknown device")
    await broadcaster.log("auth", actor, target.name, "kicked")
    await _remove_device(target.id, "kicked")
    return {"ok": True}


@router.post("/api/auth/block")
async def block(body: DeviceRequest, request: Request) -> dict:
    """Host kicks a device and blocks its fingerprint (never its IP)."""
    actor = require_host(request)
    target = hub.get_device(body.device_id)
    if target is None:
        raise ApiError(404, "unknown device")
    security.block_device(target.fingerprint, target.name)
    await broadcaster.log("auth", actor, target.name, "blocked")
    await _remove_device(target.id, "blocked")
    return {"ok": True}


@router.post("/api/auth/rotate_code")
async def rotate_code(request: Request) -> dict:
    """Host rotates the join code; the new one is returned immediately."""
    actor = require_host(request)
    new_code = hub.rotate_join_code()
    await broadcaster.log("admin", actor, "SERVER", "rotated the join code")
    return {"join_code": new_code}
