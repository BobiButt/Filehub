"""WebSocket /ws: presence, dashboard snapshot, relay signalling, progress.

One socket per browser (§7). Identity comes from the HttpOnly session
cookie — client-supplied ids/roles are never trusted (§19).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.broadcaster import broadcaster
from app.core.connection_manager import manager
from app.core.hub import hub
from app.core.relay import RelayError, relay_service
from app.core.security import SESSION_COOKIE
from app.models.schemas import DeviceState, TransferKind, TransferStatus

logger = logging.getLogger(__name__)

router = APIRouter()


def _payload(data: dict) -> dict:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


async def _relay_action(device_id: str, payload: dict, action, *extra) -> None:
    try:
        await action(str(payload.get("transfer_id", "")), device_id, *extra)
    except RelayError as exc:
        await broadcaster.error_to(device_id, exc.message)


async def _cancel_transfer(device_id: str, payload: dict) -> None:
    """cancel_transfer covers relays and the caller's own upload (§7.2)."""
    transfer_id = str(payload.get("transfer_id", ""))
    reason = str(payload.get("reason", ""))[:120] or "cancelled by sender"
    transfer = hub.get_transfer(transfer_id)
    is_relay = transfer_id in relay_service.relays or (
        transfer is not None and transfer.kind == TransferKind.RELAY
    )
    if is_relay:
        try:
            await relay_service.cancel(transfer_id, device_id, reason)
        except RelayError as exc:
            await broadcaster.error_to(device_id, exc.message)
        return
    if transfer is not None and transfer.sender_id == device_id:
        if transfer.status in (
            TransferStatus.DONE,
            TransferStatus.FAILED,
            TransferStatus.REJECTED,
            TransferStatus.CANCELLED,
        ):
            return
        transfer.status = TransferStatus.CANCELLED
        transfer.error = reason
        transfer.finished_at = datetime.now(timezone.utc)
        await broadcaster.transfer_done(transfer)  # session kept for resume
        device = hub.get_device(device_id)
        await broadcaster.log(
            TransferKind.UPLOAD,
            device.name if device else "?",
            "SERVER",
            "cancelled",
            filename=transfer.filename,
            size=transfer.size,
            transfer_id=transfer_id,
        )
        return
    await broadcaster.error_to(device_id, "unknown transfer")


async def _handle_control(device_id: str, data: dict) -> None:
    """Dispatch a client → server control message (§7.2)."""
    device = hub.get_device(device_id)
    if device is None:
        return
    kind = str(data.get("type", ""))
    payload = _payload(data)
    runtime = hub.runtime(device.id)

    if kind == "hello":
        name = " ".join(str(payload.get("name", "")).split())[:40]
        renamed = bool(name) and name != device.name
        if renamed:
            device.name = name
        if device.state == DeviceState.APPROVED:
            if not runtime.announced or renamed:
                runtime.announced = True
                await broadcaster.device_joined(device, exclude=device.id)
        elif device.state == DeviceState.PENDING and not runtime.pending_announced:
            runtime.pending_announced = True
            await broadcaster.device_pending(device)
        await broadcaster.scheduler_update()
        return

    if kind == "pong":
        return  # liveness already recorded by the receive loop

    # Everything below requires an approved member.
    if device.state != DeviceState.APPROVED:
        await broadcaster.error_to(device.id, "not approved yet", kind)
        return

    if kind == "offer_relay":
        try:
            await relay_service.create_offer(
                device.id,
                str(payload.get("to_device", "")),
                str(payload.get("filename", "")),
                int(payload.get("size", 0)),
                str(payload.get("sha256", "")),
            )
        except RelayError as exc:
            await broadcaster.error_to(device.id, exc.message, "offer_relay")
        except (TypeError, ValueError) as exc:
            await broadcaster.error_to(device.id, f"bad offer: {exc}", "offer_relay")
    elif kind == "accept_relay":
        await _relay_action(device.id, payload, relay_service.accept)
    elif kind == "reject_relay":
        reason = str(payload.get("reason", ""))[:120] or "declined"
        await _relay_action(device.id, payload, relay_service.reject, reason)
    elif kind == "finish_relay":
        await _relay_action(device.id, payload, relay_service.finish)
    elif kind == "cancel_transfer":
        await _cancel_transfer(device.id, payload)
    else:
        await broadcaster.error_to(device.id, f"unknown message type: {kind}")


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Authenticate by session cookie, then stream the live dashboard."""
    device_id = ws.cookies.get(SESSION_COOKIE, "")
    device = hub.get_device(device_id) if device_id else None
    if device is None or device.state == DeviceState.BLOCKED:
        await ws.close(code=4401)
        return

    await manager.connect(ws, device)
    runtime = hub.runtime(device.id)
    runtime.ws_connected = True
    runtime.offline_at = None
    await manager.send_to(
        device.id, {"type": "snapshot", "payload": hub.snapshot(device.role, device.id)}
    )
    logger.debug("WS session open for %r", device.name)

    try:
        while True:
            message = await ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            manager.touch(device.id)
            hub.touch_device(device.id)
            if message.get("bytes") is not None:
                try:
                    await relay_service.forward_chunk(message["bytes"], device.id)
                except RelayError as exc:
                    await broadcaster.error_to(device.id, exc.message)
                except Exception:  # noqa: BLE001 - malformed frame, keep socket
                    logger.warning("Malformed relay frame from %r", device.name)
                    await broadcaster.error_to(device.id, "malformed relay frame")
            elif message.get("text") is not None:
                try:
                    data = json.loads(message["text"])
                    if not isinstance(data, dict):
                        raise ValueError("not an object")
                except ValueError:
                    await broadcaster.error_to(device.id, "invalid JSON message")
                    continue
                try:
                    await _handle_control(device.id, data)
                except Exception:  # noqa: BLE001 - keep the socket alive
                    logger.exception("Error handling WS message from %r", device.name)
                    await broadcaster.error_to(
                        device.id, "server error handling message"
                    )
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass  # socket closed underneath us
    finally:
        removed = await manager.disconnect(device.id, ws)
        runtime = hub.runtime(device.id)
        if removed and hub.get_device(device.id) is not None:
            runtime.ws_connected = False
            runtime.offline_at = time.monotonic()
            # Relay cannot continue without a socket: abort now (§11.3).
            # Presence lingers for the heartbeat grace period, then device_left.
            await relay_service.abort_for_device(device.id, "device disconnected")
            logger.debug("WS session closed for %r (grace period)", device.name)
